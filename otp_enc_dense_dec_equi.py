import torch
import torch.nn
import torch.nn.functional
import torch.optim
import torch.utils.data

from time import perf_counter
from tqdm import tqdm
from argparse import ArgumentParser

from cgae.gs import gumbel_softmax
from cgae.cgae import temp_scheduler
from cgae.equi import Decoder, nearest_assignment
from cgae.cgae_dense import Encoder

import otp


parser = ArgumentParser(
    parents=[otp.otp_parser(), otp.otp_equi_parser()], add_help=True
)
args = otp.parse_args(parser)


def execute(args):
    geometries, forces, features = otp.data(args)

    if args.cg_ones:
        cg_features = torch.ones(
            args.bs, args.ncg, 1, dtype=args.precision, device=args.device
        )
    else:
        cg_features = torch.zeros(
            args.bs, args.ncg, args.ncg, dtype=args.precision, device=args.device
        )
        cg_features.scatter_(
            -1,
            torch.arange(args.ncg, device=args.device)
            .expand(args.bs, args.ncg)
            .unsqueeze(-1),
            1.0,
        )

    encoder = Encoder(
        in_dim=geometries.size(1), out_dim=args.ncg, hard=False, device=args.device
    ).to(args.device)
    decoder = Decoder(args).to(device=args.device)
    optimizer = torch.optim.Adam(
        list(encoder.parameters()) + list(decoder.parameters()), lr=args.lr
    )
    temp_sched = temp_scheduler(
        args.epochs,
        args.tdr,
        args.temp,
        args.tmin,
        dtype=args.precision,
        device=args.device,
    )
    n_batches, geometries, forces, features = otp.batch(
        geometries, forces, features, args.bs
    )

    dynamics = []
    summaries = []
    wall_start = perf_counter()
    torch.manual_seed(args.seed)
    for epoch in tqdm(range(args.epochs)):
        for step, batch in tqdm(
            enumerate(torch.randperm(n_batches, device=args.device))
        ):
            feat, geo, force = features[batch], geometries[batch], forces[batch]

            # Auto encoder
            cg_xyz = encoder(geo, temp_sched[epoch])
            logits = encoder.weight1.t()
            cg_assign, st_cg_assign = gumbel_softmax(
                logits, temp_sched[epoch], dtype=args.precision, device=args.device
            )

            # End goal is projection of atoms by atomic number onto coarse grained atom.
            relative_xyz = (
                geo.unsqueeze(2).cpu().detach() - cg_xyz.unsqueeze(1).cpu().detach()
            )
            nearest_assign = nearest_assignment(cg_xyz, geo)
            if args.gumble_sm_proj:
                cg_proj = otp.project_onto_cg(relative_xyz, cg_assign, feat, args)
            elif args.nearest:
                cg_proj = otp.project_onto_cg(relative_xyz, nearest_assign, feat, args)
            else:
                cg_proj = otp.project_onto_cg(relative_xyz, st_cg_assign, feat, args)

            pred_sph = decoder(cg_features, cg_xyz.clone().detach())
            cg_proj = cg_proj.reshape_as(pred_sph)
            loss_ae = (cg_proj - pred_sph).pow(2).sum(-1).div(args.atomic_nums).mean()

            if args.fm and epoch >= args.fm_epoch:
                # Force matching
                cg_force_assign, _ = gumbel_softmax(
                    logits,
                    temp_sched[epoch] * args.force_temp_coeff,
                    device=args.device,
                    dtype=args.precision,
                )
                cg_force = torch.einsum("...ij,zik->zjk", cg_force_assign, force)
                loss_fm = cg_force.pow(2).sum(-1).mean()

                loss = loss_ae + args.fm_co * loss_fm
            else:
                loss_fm = torch.tensor(0)
                loss = loss_ae

            dynamics.append(
                {
                    "loss_ae": loss_ae.item(),
                    "loss_fm": loss_fm.item(),
                    "loss": loss.item(),
                    "epoch": epoch,
                    "step": step,
                    "batch": batch.item(),
                }
            )

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        wall = perf_counter() - wall_start
        if wall > args.wall:
            break

        summaries.append(
            {
                "loss_ae": loss_ae.item(),
                "loss_fm": loss_fm.item(),
                "loss": loss.item(),
                "epoch": epoch,
                "step": step,
                "batch": batch.item(),
                "cg_xyz": cg_xyz,
                "pred_sph": pred_sph,
                "sph": cg_proj,
                "temp": temp_sched[epoch].item(),
                "gumble": cg_assign,
                "st_gumble": st_cg_assign,
                "nearest": nearest_assign,
            }
        )

    return {
        "args": args,
        "dynamics": dynamics,
        "summaries": summaries,
        # 'train': {
        #     'pred': evaluate(f, features, geometry, train[:len(test)]),
        #     'true': forces[train[:len(test)]],
        # },
        # 'test': {
        #     'pred': evaluate(f, features, geometry, test[:len(train)]),
        #     'true': forces[test[:len(train)]],
        # },
        "encoder": encoder.state_dict() if args.save_state else None,
        "decoder": decoder.state_dict() if args.save_state else None,
    }


def main():
    results = execute(args)
    with open(args.pickle, "wb") as f:
        torch.save(results, f)


if __name__ == "__main__":
    main()
